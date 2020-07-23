import problemtools
import yaml
import os
import re
import shlex

BASENAME_PATTERN = re.compile('^[a-zA-Z0-9][a-zA-Z0-9_.-]*[a-zA-Z0-9]$')

class ValidationError(Exception):
    def __init__(self, msg, path):
        self.msg = msg
        self.path = path

    def __str__(self):
        return '%s at %s' % (self.msg, self.path if self.path else '/')

class Optional:
    def __init__(has_value, value):
        self.has_value = has_value
        self.value = value

    @staticmethod
    def some(value):
        return Optional(True, value)

    @staticmethod
    def none():
        return Optional(False, None)

    def fallback(other):
        if self.has_value:
            return self
        else:
            return other

    def __repr__(self):
        if self.has_value:
            return 'Optional.value(%s)' % repr(self.value)
        else:
            return 'Optional.none()'

class Counter:
    def __init__(self):
        self.cnt = 0

    def next(self):
        self.cnt += 1
        return self.cnt

class Command:
    COMMAND_PATTERN = re.compile(r'^[^{}]*(\{[^{}]*\}[^{}]*)*$')

    def __init__(self, original):
        self.original = original

    def _compute_seed(self, random_salt):
        return int(hashlib.sha512((random_salt + self.original).encode('utf-8')).hexdigest(), 16) % (2**31)

    def _interpolate(self, pattern, name, random_salt):
        seed = None
        res = []
        first = True
        for group in pattern.split('{'):
            parts = group.split('}')
            if first:
                first = False
                rem = parts[0]
            else:
                group, rem = parts
                if group == 'name':
                    res.append(name)
                elif group.startswith('seed'):
                    if seed is None:
                        seed = str(self._compute_seed(random_salt))
                    res.append(seed)
                else:
                    raise Exception("unexpected interpolation group '%s' in command" % group)
            res.append(rem)
        return ''.join(res)

    @staticmethod
    def parse(data, path):
        if not isinstance(data, str):
            raise ValidationError('command should be a string', path)

        parts = shlex.split(data)
        if not parts:
            raise ValidationError('invalid command', path)

        for part in parts:
            if not COMMAND_PATTERN.match(part):
                raise ValidationError('invalid command', path)
            for group in part.split('{')[1:]:
                group = group.split('}')[0]
                if not (group == 'name' or
                        group == 'seed' or
                        group.startswith('seed:')):
                    raise ValidationError('invalid command', path)

        if len(parts) == 1 and parts[0].endswith('.in'):
            commands.append(CopyCommand(
                path=parts[0],
                original=data,
            ))
        else:
            commands.append(ProgramCommand(
                program=parts[0],
                arguments=parts[1:],
                original=data,
            ))

class CopyCommand(Command):
    def __init__(self, path, original):
        super(Command, self).__init__(original)
        self.path = path
        self.original = original

    def __repr__(self):
        return 'CopyCommand(%s, %s)' % (repr(self.path), repr(self.original))

class ProgramCommand(Command):
    def __init__(self, program, arguments, original):
        super(Command, self).__init__(original)
        self.program = program
        self.arguments = arguments

    def __repr__(self):
        return 'ProgramCommand(%s, %s, %s)' % (repr(self.program), repr(self.arguments), repr(self.original))

class TestcaseConfig:
    def __init__(self,
            solution=Optional.none(),
            visualizer=Optional.none(),
            random_salt=Optional.none()):
        self.solution = solution
        self.visualizer = visualizer
        self.random_salt = random_salt

    @staticmethod
    def extract(data, path):
        remaining = {}

        solution = Optional.none()
        visualizer = Optional.none()
        random_salt = Optional.none()

        for key, value in data.items():
            if key == 'solution':
                if value is not None and not isinstance(value, str):
                    raise ValidationError("invalid type '%s' of solution" % type(value), path)
                solution = Optional.value(None if value is None else Command.parse(value, '%s/%s' % (path, key)))
            elif key == 'visualizer':
                if value is not None and not isinstance(value, str):
                    raise ValidationError("invalid type '%s' of visualizer" % type(value), path)
                visualizer = Optional.value(None if value is None else Command.parse(value, '%s/%s' % (path, key)))
            elif key == 'random_salt':
                if not isinstance(value, str):
                    raise ValidationError("invalid type '%s' of random_salt" % type(value), path)
                random_salt = Optional.value(value)
            else:
                remaining[key] = value
        return TestcaseConfig(
                solution=solution,
                visualizer=visualizer,
                random_salt=random_salt,
            ), remaining

    def override(self, other):
        return TestcaseConfig(
            solution=other.solution.fallback(self.solution),
            visualizer=other.visualizer.fallback(self.visualizer),
            random_salt=other.random_salt.fallback(self.random_salt),
        )

    def __repr__(self):
        return 'TestcaseConfig(%s, %s, %s)' % (repr(self.solution), repr(self.visualizer), repr(self.random_salt))

class Cases:
    @staticmethod
    def parse(data, path):
        if data is None:
            data = {}
        if isinstance(data, list):
            cases = []
            for item in data:
                if not isinstance(item, dict):
                    raise ValidationError("each list item in 'data' must be a dictionary", path)
                if not item:
                    raise ValidationError("unexpected empty dictionary in 'data' list item", path)
                cases.append({
                    name: TestElement.parse(name, value, path)
                    for name, value in item.items()
                })
            return cases
        elif isinstance(data, dict):
            cases = {}
            for key, value in data.items():
                cases[key] = TestElement.parse(key, value, path)
            return cases
        else:
            raise ValidationError('data must be either a dictionary or a list', path)

class Include:
    @staticmethod
    def parse(data, path):
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValidationError("expected a dictionary, got '%s'" % type(data), path)
        for key, value in data.items():
            if not isinstance(key, str):
                raise ValidationError("expected a string key, got '%s'" % type(key), path)
            if not isinstance(value, list):
                raise ValidationError("expected a list value, got '%s'" % type(value), path)
            if not value:
                raise ValidationError('list must be nonempty', path)

class Directory:
    TYPE = 'directory'

    def __init__(self, name, testcase_config, testdata_yaml, cases):
        self.name = name
        self.testcase_config = testcase_config
        self.testdata_yaml = testdata_yaml
        self.cases = cases

    def __repr__(self):
        return 'Directory(%s, %s, %s, %s)' % (repr(self.name), repr(self.testcase_config), repr(self.testdata_yaml), repr(self.cases))

    @staticmethod
    def parse(name, data, path):
        if not isinstance(data, dict):
            raise ValidationError('directory value must be a dictionary', path)
        if 'type' in data and data['type'] != Directory.TYPE:
            raise ValidationError('directory must have type=%s' % Directory.TYPE, path)
        if not isinstance(name, str) or not BASENAME_PATTERN.match(name):
            raise ValidationError("invalid directory name '%s'" % name, path)

        testcase_config, data = TestcaseConfig.extract(data, path)
        testdata_yaml = None
        include = []
        cases = {}
        for key, value in data.items():
            if key == 'type':
                pass
            elif key == 'data':
                cases = Cases.parse(value, path)
            elif key == 'testdata.yaml':
                if value is None:
                    value = {}
                if not isinstance(value, dict):
                    raise ValidationError('testdata.yaml value must be a dictionary', path)
                testdata_yaml = value
            elif key == 'include':
                include = Include.parse(value, '%s/%s' % (path, key))
            else:
                # raise ValidationError("unrecognized configuration key '%s'" % key, path)
                # We allow tooling-specific configuration keys
                pass
        return Directory(name,
                         testcase_config=testcase_config,
                         testdata_yaml=testdata_yaml,
                         include=include,
                         cases=cases)

    def add_prefix(self, prefix):
        self.name = prefix + self.name

    # def label_lists(self, counter=None, is_ordered=None, path='', width=None):
    #     if width is not None:
    #         return

    #     counter = Counter()
    #     is_ordered = isinstance(self.cases, list)
    #     HasCases.label_lists(self, counter, is_ordered, path)

    #     if is_ordered:
    #         width = len(str(counter.cnt))
    #         counter = Counter()
    #         HasCases.label_lists(self, counter, is_ordered, path, width)

    def label_lists(self, counter, is_ordered, path, width=None):
        if is_ordered and isinstance(self.cases, dict):
            raise ValidationError('found unordered data in an ordered directory', path)
        if not is_ordered and isinstance(self.cases, list):
            raise ValidationError('found ordered data in an unordered directory', path)

        if is_ordered:
            new_cases = {}
            for item in self.cases:
                key, value = next(iter(item.items()))

                if not isinstance(value, Scope):
                    index = counter.next()
                    if width is not None:
                        prefix = str(index).rjust(width, '0') + '-'
                        value.add_prefix(prefix)
                        if not isinstance(value, Directory):
                            key = prefix + key

                if width is not None:
                    if key in new_cases:
                        raise ValidationError("duplicate key '%s'" % key, path)
                    new_cases[key] = value

                if isinstance(value, HasCases):
                    value.label_lists(counter, is_ordered, '%s/%s' % (path, key), width)
            if width is not None:
                self.cases = new_cases
        else:
            for key, value in self.cases.items():
                if isinstance(value, HasCases):
                    value.label_lists(counter, is_ordered, '%s/%s' % (path, key), width)

class Testcase:
    TYPE = 'testcase'

    def __init__(self, name, manual, command, config):
        self.name = name
        self.manual = manual
        self.command = command
        self.config = config

    def __repr__(self):
        return 'Testcase(%s, %s, %s, %s)' % (repr(self.name), repr(self.manual), repr(self.command), repr(self.config))

    @staticmethod
    def parse(name, data, path):
        if not isinstance(name, str) or not BASENAME_PATTERN.match(name):
            raise ValidationError("invalid testcase name '%s'" % name, path)

        if data is None:
            return Testcase(name,
                            manual=True,
                            command=None,
                            config=None)
        elif isinstance(data, str):
            return Testcase(name,
                            manual=False,
                            command=None,
                            config=None)
        elif isinstance(data, dict):
            testcase_config, data = TestcaseConfig.extract(data, path)

            if 'input' not in data:
                raise ValidationError("missing key 'input'", path)

            return Testcase(name,
                            manual=False,
                            command=Command.parse(data['input'], path),
                            config=testcase_config)
        else:
            raise ValidationError('unexpected testcase value of type %s' % type(data), path)

    def add_prefix(self, prefix):
        self.prefix = prefix + self.prefix

class TestElement:
    @staticmethod
    def parse(name, data, path):
        if not isinstance(name, str) or not BASENAME_PATTERN.match(name):
            raise ValidationError("invalid test element name '%s'" % name, path)

        if isinstance(data, dict) and data.get('type') == Directory.TYPE:
            return Directory.parse(name, data, '%s/%s' % (path, name))

        return Testcase.parse(name, data, '%s/%s' % (path, name))

class Generators:
    @staticmethod
    def parse(data, path):
        pass

class GeneratorsYaml:
    def __init__(self, generators, testcase_config, data):
        self.generators = generators
        self.data = data

    @staticmethod
    def parse(data, path=''):
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValidationError('top-level element must be a dictionary', path)
        if 'type' in data:
            raise ValidationError("'type' is not a valid key in the top-level dictionary", path)
        if 'include' in data:
            raise ValidationError("'include' is not a valid key in the top-level dictionary", path)

        new_data = {
            'type': Directory.TYPE,
        }
        generators = {}
        for key, value in remaining.items():
            if key == 'generators':
                generators = Generators.parse(value, '%s/%s' % (path, key))
            else:
                new_data[key] = value

        return GeneratorsYaml(generators=generators,
                              data=Directory.parse('data', new_data, path))

def main():
    pass

if __name__ == '__main__':
    pass

