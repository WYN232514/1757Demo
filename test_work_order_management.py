import tempfile
import unittest
from pathlib import Path

from work_order_management import exclusions, exclude_work_order


class WorkOrderManagementTests(unittest.TestCase):
    def test_persistence_and_deletion_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "management.sqlite3"
            self.assertEqual(exclusions(path), set())
            exclude_work_order(path, "5001727589", "operator", exclude_recommendation=True)
            exclude_work_order(path, "5001725262", "operator", exclude_recommendation=False)
            self.assertEqual(exclusions(path), {"5001727589", "5001725262"})
            self.assertEqual(exclusions(path, recommendation_only=True), {"5001727589"})
            # Repeated submissions cannot change the originally confirmed scope.
            exclude_work_order(path, "5001725262", "operator", exclude_recommendation=True)
            self.assertEqual(exclusions(path, recommendation_only=True), {"5001727589"})

    def test_invalid_id_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "management.sqlite3"
            with self.assertRaises(ValueError):
                exclude_work_order(path, "5001727589 OR 1=1", "operator", exclude_recommendation=True)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
