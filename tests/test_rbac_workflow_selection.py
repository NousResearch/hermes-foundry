import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('selector', Path(__file__).parents[1] / 'scripts/select_rbac_workflow.py')
selector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selector)


class SelectionTests(unittest.TestCase):
    def test_names_are_stable_case_insensitive_and_target_specific(self):
        project = '/subscriptions/example/resourceGroups/group/providers/Microsoft.CognitiveServices/accounts/account/projects/project'
        name = selector.scoped_name(project, 'blueprint-a')
        self.assertEqual(name, selector.scoped_name(project.upper(), 'BLUEPRINT-A'))
        self.assertNotEqual(name, selector.scoped_name(project, 'blueprint-b'))
        self.assertNotEqual(name, selector.scoped_name(project + '-other', 'blueprint-a'))
        self.assertLessEqual(len(name) + 15, 64)  # ARM deployment name including timestamp

    def test_unreadable_legacy_fails_closed(self):
        def denied(_):
            raise RuntimeError('access denied')
        with self.assertRaises(RuntimeError):
            selector.select('/subscriptions/example/providers/service/project', 'blueprint', [{'name': selector.LEGACY}], denied)

    def test_invalid_inventory_fails_closed(self):
        with self.assertRaises(ValueError):
            selector.select('/subscriptions/example/providers/service/project', 'blueprint', {}, lambda _: {})
