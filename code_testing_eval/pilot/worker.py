"""Trusted worker copied read-only into a fresh sandbox; no private bank data here."""
import json
import pathlib
import sys
import traceback
import unittest


def main():
    root = pathlib.Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(root))
    import click
    if not pathlib.Path(click.__file__).resolve().is_relative_to(root / 'click'):
        raise RuntimeError('Wrong Click source')
    loader = unittest.TestLoader()
    suite = loader.discover(str(root / 'submissions'), pattern='test_*.py')
    if loader.errors:
        print(json.dumps({'phase': 'collection_error', 'errors': loader.errors}))
        return
    if not 1 <= suite.countTestCases() <= 128:
        print(json.dumps({'phase': 'collection_error', 'errors': ['Require 1–128 test cases']}))
        return

    class Result(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.rows = []
            self.current = None

        def startTest(self, test):
            super().startTest(test)
            self.current = {'id': test.id(), 'status': 'passed', 'click_called': False, 'details': []}
            def profile(frame, event, arg):
                if event == 'call' and frame.f_code.co_filename.startswith(str(root / 'click') + '/'):
                    self.current['click_called'] = True
            sys.setprofile(profile)

        def detail(self, error):
            typ, value, tb = error
            frames = traceback.extract_tb(tb)
            return {'type': typ.__name__, 'message': str(value),
                    'setup_error': any(f.name in {'_callSetUp', '_callTearDown'} and 'unittest' in f.filename for f in frames),
                    'click_frame': any(str(root / 'click') + '/' in f.filename for f in frames),
                    'traceback': ''.join(traceback.format_exception(typ, value, tb)).replace(str(root), '<WORKSPACE>')[-12000:]}

        def addFailure(self, test, error):
            super().addFailure(test, error)
            self.current['status'] = 'failed'
            self.current['details'].append(self.detail(error))

        def addError(self, test, error):
            super().addError(test, error)
            if self.current is None:
                detail = self.detail(error)
                detail['setup_error'] = True
                self.rows.append({'id': test.id(), 'status': 'error', 'click_called': False, 'details': [detail]})
                return
            self.current['status'] = 'error'
            self.current['details'].append(self.detail(error))

        def addSubTest(self, test, subtest, error):
            super().addSubTest(test, subtest, error)
            if error is not None:
                self.current['status'] = 'failed' if issubclass(error[0], AssertionError) else 'error'
                self.current['details'].append(self.detail(error))

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            if self.current is None:
                self.rows.append({'id': test.id(), 'status': 'skipped', 'click_called': False, 'details': []})
                return
            self.current['status'] = 'skipped'

        def stopTest(self, test):
            sys.setprofile(None)
            self.rows.append(self.current)
            self.current = None
            super().stopTest(test)

    result = Result()
    suite.run(result)
    print('\nPILOT_RESULT=' + json.dumps({'phase': 'tests', 'tests': result.rows}, sort_keys=True))


if __name__ == '__main__':
    main()
