#! /usr/bin/env python2
# -*- coding: utf-8 -*-
from __future__ import print_function
import tempfile
import yaml
import sys
import os
import re
import logging
import shutil
import shlex
import hashlib
from copy import deepcopy
from argparse import ArgumentParser, ArgumentTypeError


from . import languages
from . import run


def rmtree_or_unlink_if_exists(path):
    """Delete the specified file or directory, if it exists."""
    if os.path.exists(path):
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)

def ensure_directory(path, recursive=True):
    """Ensure that the specified path points to a directory, creating
       directories and removing files if necessary.

    Args:
        recursive (bool): if false, then everything up to the last name in the
                          path is assumed to represent an existing directory.
    """
    
    create = []
    while not os.path.isdir(path):
        path, dirname = os.path.split(path)
        create.append(dirname)
        if not recursive:
            break

    for dirname in create[::-1]:
        path = os.path.join(path, dirname)
        rmtree_or_unlink_if_exists(path)
        os.mkdir(path)

class GeneratorError(Exception):
    pass

class ProblemAspect:
    def error(self, msg, additional_info=None):
        logging.error('in %s: %s', self, msg)
        raise GeneratorError()

    def warning(self, msg, additional_info=None):
        logging.warning('in %s: %s', self, msg)

    def msg(self, msg):
        print(msg)

    def info(self, msg):
        logging.info(': %s', msg)

    def debug(self, msg):
        logging.debug(': %s', msg)

class Problem(ProblemAspect):
    def __init__(self, probdir):
        self.probdir = os.path.realpath(probdir)
        self.shortname = os.path.basename(self.probdir)
        self.language_config = languages.load_language_config()

        self.data_path = os.path.join(self.probdir, 'data')
        self.generator_path = os.path.join(self.probdir, 'generators')
        self.manifestfile = os.path.join(self.generator_path, 'gen.yaml')

    def __enter__(self):
        if not os.path.isdir(self.probdir):
            self.error("Problem directory '%s' not found" % self.probdir)
        if not os.path.isdir(self.generator_path):
            self.error("Generator directory '%s' not found" % self.generator_path)
        if not os.path.isfile(self.manifestfile):
            self.error("Generator manifest '%s' not found" % self.manifestfile)

        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        pass

    def __str__(self):
        return self.shortname

class GeneratedKind:
    TESTCASE = 1
    TESTGROUP = 2
    TESTDATA_YAML = 3

class GeneratorManifest(ProblemAspect):
    _DEFAULT_CONFIG = {
        'extensions': {
        },
    }

    key_regex = re.compile('^[a-zA-Z0-9][a-zA-Z0-9_.-]*[a-zA-Z0-9]$')

    def __init__(self, problem):
        self.problem = problem

    def __str__(self):
        return 'gen.yaml'

    def __enter__(self):
        self.tmpdir = tempfile.mkdtemp(prefix='gendata-%s-'%self.problem.shortname)
        self.generator_cache = {}

        with open(self.problem.manifestfile, 'r') as f:
            try:
                self.manifest = yaml.load(f, Loader=yaml.Loader)
            except yaml.YAMLError as e:
                self.error('invalid YAML: %s' % e)

        if not isinstance(self.manifest, dict):
            self.error('expected top-level dictionary, got %s' % type(self.manifest))

        paths = set()
        def handler(kind, path, gen, config):
            if path in paths:
                self.error('multiple declarations for path %s' % self._yaml_path(path))
            paths.add(path)
        self._walk(handler)

        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        shutil.rmtree(self.tmpdir)

    def _get_generator(self, path):
        if path not in self.generator_cache:
            work_dir = tempfile.mkdtemp(dir=self.tmpdir, prefix='generator-')
            program = run.get_program(path,
                                      language_config=self.problem.language_config,
                                      work_dir=work_dir)

            try:
                success, msg = program.compile()
                if not success:
                    self.error('Compile error for %s' % program, msg)
            except run.ProgramError as e:
                self.error(e)

            self.generator_cache[path] = program

        return self.generator_cache[path]

    def _yaml_path(self, path):
        return os.path.join('/', os.path.relpath(path, self.problem.data_path).lstrip('.'))

    def _validate_generator(self, gen, path, allow_none=False):
        if gen is None and allow_none:
            return

        if not isinstance(gen, list):
            gen = [gen]

        if not gen:
            self.error('unexpected empty generator list at %s' % self._yaml_path(path))

        for command in gen:
            if not isinstance(command, str):
                self.error('unexpected type %s of command at %s' % (type(command), self._yaml_path(path)))

            command = shlex.split(command)
            if not command:
                self.error('unexpected empty command at %s' % self._yaml_path(path))

    def _validate_config(self, config, path):
        if not isinstance(config, dict):
            self.error('config at %s is not a dictionary' % self._yaml_path(path))

        for key, value in config.items():
            if key == 'extensions':
                for ext, gen in value.items():
                    if ext == 'in':
                        self.error('forbidden extension .in in extensions config at %s' % self._yaml_path(path))
                    if gen == None:
                        # Special value for stating that this extension is not generated
                        continue
                    if gen == 'generated':
                        # Special value meaning that this extension is externally generated
                        continue

                    self._validate_generator(gen, os.path.join(path, 'extensions', key))
            else:
                self.warning('unknown key %s in config at %s' % (key, self._yaml_path(path)))

    def _merge_configs(self, old_config, new_config):
        config = deepcopy(old_config)
        for key, value in new_config.items():
            if key == 'extensions':
                for ext, gen in value.items():
                    if not gen:
                        del config[key][ext]
                    else:
                        config[key][ext] = gen
            else:
                config[key] = value
        return config

    def _validate_testdata_yaml(self, testdata_yaml, path):
        if not isinstance(testdata_yaml, dict):
            self.error('expected a dictionary in testdata.yaml at %s' % self._yaml_path(path))

    def _walk_group(self, manifest, path, parent_config, handler, post_group_handler):
        assert isinstance(manifest, dict), "manifest should always be a dictionary"

        scope_config = manifest.get('config', {})
        self._validate_config(scope_config, path)
        config = self._merge_configs(parent_config, scope_config)

        for key, value in manifest.items():
            if not key:
                self.error('unexpected empty key at %s' % self._yaml_path(path))
            if key == 'config':
                continue
            if not self.key_regex.match(key):
                self.error('invalid key %s at %s' % (key, self._yaml_path(path)))

            if key == 'testdata.yaml':
                self._validate_testdata_yaml(value, path)
                kind = GeneratedKind.TESTDATA_YAML
            elif isinstance(value, dict):
                # We have a group, so we recurse
                self._walk_group(value, os.path.join(path, key), config, handler, post_group_handler)
                continue
            else:
                self._validate_generator(value, os.path.join(path, key), allow_none=True)
                kind = GeneratedKind.TESTCASE if key.endswith('.in') else GeneratedKind.TESTGROUP

            handler(kind, os.path.join(path, key), value, config)
            if kind == GeneratedKind.TESTGROUP and post_group_handler is not None:
                post_group_handler({}, os.path.join(path, key), config)

        if post_group_handler is not None:
            post_group_handler(manifest, path, config)

    def _walk(self, handler, post_group_handler=None):
        self._walk_group(self.manifest, self.problem.data_path, GeneratorManifest._DEFAULT_CONFIG, handler, post_group_handler)

    def _hash_command(self, command):
        return str(int(hashlib.sha512(command.encode('utf-8')).hexdigest(), 16) % (2**31))

    def _parse_command(self, command, gen_path, path):
        seed = self._hash_command(command)

        command = shlex.split(command)
        program_path, arguments = command[0], command[1:]
        program_path = os.path.join(gen_path, program_path)

        for i in range(len(arguments)):
            if arguments[i] == '$PATH':
                arguments[i] = path
            elif arguments[i].startswith('$SEED'):
                arguments[i] = seed

        return program_path, arguments

    def _execute_generator(self, gen, gen_path, tmp_dir, gen_dir, path, input_filename=None, output_filename=None):
        if not isinstance(gen, list):
            gen = [gen]

        input_path = None
        if input_filename is not None:
            input_path = os.path.join(gen_dir, input_filename)

        for command in gen:
            program_path, arguments = self._parse_command(command, gen_path, path)
            generator = self._get_generator(program_path)

            output_path = os.path.join(tmp_dir, '_tmp_output')

            args = {}
            if arguments:
                args['args'] = arguments
            if input_path is not None:
                args['infile'] = input_path
            if output_path is not None:
                args['outfile'] = output_path

            old_wd = os.getcwd()
            os.chdir(gen_dir)
            print(args)
            status, runtime = generator.run(**args)
            os.chdir(old_wd)

            returncode = os.WEXITSTATUS(status)
            if returncode != 0:
                self.error('generator %s terminated with error %d' % (command, returncode))

            os.chmod(output_path, 420)
            input_path = os.path.join(tmp_dir, '_tmp_input')
            rmtree_or_unlink_if_exists(input_path)
            shutil.move(output_path, input_path)

        if output_filename is None:
            rmtree_or_unlink_if_exists(input_path)
        else:
            output_path = os.path.join(gen_dir, output_filename)
            rmtree_or_unlink_if_exists(output_path)
            shutil.move(input_path, output_path)

    def _generate_extensions(self, path, config, tmp_dir=None):
        assert path.endswith('.in'), 'testcase paths should always end with .in'

        old_extensions = ['in']
        new_extensions = []
        for ext, gen in config['extensions'].items():
            if gen == 'generated':
                old_extensions.append(ext)
            elif ext == 'ans':
                # If present, .ans should be the first extension that is generated
                new_extensions = ['ans'] + new_extensions
            else:
                new_extensions.append(ext)

        existing_tmp_dir = tmp_dir is not None
        if not new_extensions and not existing_tmp_dir:
            # No new extensions to be generated, let's exit early
            return

        copy_old = not existing_tmp_dir
        if not existing_tmp_dir:
            tmp_dir = tempfile.mkdtemp(dir=self.tmpdir, prefix='%s-ext-'%os.path.basename(path))

        try:
            gen_dir = os.path.join(tmp_dir, 'gen')
            if not existing_tmp_dir:
                os.mkdir(gen_dir)

            if copy_old:
                for ext in old_extensions:
                    cur_path = path[:-2] + ext
                    if os.path.isfile(cur_path):
                        shutil.copy(cur_path, gen_dir)

            for ext in new_extensions:
                cur_path = path[:-2] + ext
                self._execute_generator(gen, self.problem.probdir, tmp_dir, gen_dir, os.path.basename(cur_path),
                                        input_filename = os.path.basename(path),
                                        output_filename = os.path.basename(cur_path))

            if not existing_tmp_dir:
                # TODO: Separate function move_generated
                ensure_directory(os.path.dirname(path))
                for ext in old_extensions + new_extensions:
                    to_path = path[:-2] + ext
                    from_path = os.path.join(gen_dir, os.path.basename(to_path))
                    rmtree_or_unlink_if_exists(to_path)
                    shutil.move(from_path, to_path)

        finally:
            if not existing_tmp_dir:
                shutil.rmtree(tmp_dir)

    def _run_post_group(self, manifest, path, config):
        for fname in os.listdir(path):
            if fname in manifest:
                # Cases in manifest have already been handled
                continue
            if os.path.isdir(fname):
                self._run_post_group({}, os.path.join(path, fname), config)
            elif os.path.isfile(fname) and fname.endswith('.in'):
                self._generate_extensions(os.path.join(path, fname), config)

    def _run_single(self, kind, path, gen, config):
        target_dir = path if kind == GeneratedKind.TESTGROUP else os.path.dirname(path)
        ensure_directory(target_dir)

        if kind == GeneratedKind.TESTDATA_YAML:
            with open(path, 'w') as f:
                yaml.dump(gen, f)
            return

        if gen is None:
            # Manual case: input file(s) are already in the data directory
            return

        tmp_dir = tempfile.mkdtemp(dir=self.tmpdir, prefix='%s-'%os.path.basename(path))

        try:
            gen_dir = os.path.join(tmp_dir, 'gen')
            os.mkdir(gen_dir)

            self._execute_generator(gen, self.problem.generator_path, tmp_dir, gen_dir, os.path.basename(path),
                                    output_filename = os.path.basename(path) if kind == GeneratedKind.TESTCASE else None)

            # Move all generated files to the target dir, overwriting if necessary
            for root, dirs, files in os.walk(gen_dir):
                cur_path = os.path.relpath(root, gen_dir)
                cur_target_dir = os.path.join(target_dir, cur_path)

                files = set(files)
                for fname in files:
                    if '.' not in fname:
                        continue
                    ext = fname.split('.')[-1]
                    if not (ext == 'in' or config['extensions'].get(ext) == 'generated'):
                        # Only move .in files and corresponding generated extensions
                        continue

                    in_file = fname[:-len(ext)] + 'in'
                    if in_file not in files:
                        # Don't move generated extension if the corresponding .in file was not generated
                        # (we would lose track of this file in the data directory)
                        continue

                    ensure_directory(cur_target_dir, recursive=False)
                    cur_target = os.path.join(cur_target_dir, fname)
                    rmtree_or_unlink_if_exists(cur_target)
                    shutil.move(os.path.join(root, fname), cur_target)

        finally:
            shutil.rmtree(tmp_dir)

    def run(self):
        self._walk(self._run_single, self._run_post_group)

    def _clean_single(self, kind, path, gen, config):
        if kind == GeneratedKind.TESTCASE:
            assert path.endswith('.in'), 'testcase paths should always end with .in'

            remove = [path]
            for ext in config['extensions']:
                remove.append('%s.%s' % (path[:-3], ext))

            for remove_path in remove:
                if os.path.exists(remove_path):
                    try:
                        os.unlink(remove_path)
                    except:
                        self.warning('could not remove file %s' % remove_path)

        elif kind == GeneratedKind.TESTDATA_YAML:
            if gen is not None and os.path.exists(path):
                try:
                    os.unlink(path)
                except:
                    self.warning('could not remove file %s' % path)

        elif kind == GeneratedKind.TESTGROUP:
            if gen is not None and os.path.exists(path):
                try:
                    shutil.rmtree(path)
                except:
                    self.warning('could not remove directory %s' % path)

    def clean(self):
        self._walk(self._clean_single)

def argparser():
    parser = ArgumentParser(description='Generate test data for a problem package in the Kattis problem format.')
    parser.add_argument('-c', '--clean',
                        action='store_true',
                        help='clean generated input and answer files')
    parser.add_argument('-l', '--log_level',
                        default='warning',
                        help='set log level (debug, info, warning, error, critical)')
    parser.add_argument('problemdir')
    return parser

def main():
    args = argparser().parse_args()

    fmt = "%(levelname)s %(message)s"
    logging.basicConfig(stream=sys.stdout,
                        format=fmt,
                        level=eval("logging." + args.log_level.upper()))


    try:
        with Problem(args.problemdir) as problem:
            with GeneratorManifest(problem) as gen:
                if args.clean:
                    gen.clean()
                else:
                    gen.run()
    except GeneratorError:
        sys.exit(1)

if __name__ == '__main__':
    main()
