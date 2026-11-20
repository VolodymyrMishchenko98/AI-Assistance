import ast
import unittest
from pathlib import Path


class BoundaryTests(unittest.TestCase):
    def test_domain_does_not_import_outer_layers(self) -> None:
        root = Path(__file__).parents[1] / "src" / "ai_assistance"
        domain = root / "modules" / "price_checker" / "domain"
        forbidden = ("infrastructure", "presentation", "fastapi", "sqlalchemy", "httpx", "sqlite3")
        for path in domain.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
            imported += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
            self.assertFalse(any(any(part in name.lower() for part in forbidden) for name in imported), path)

    def test_presentation_does_not_import_infrastructure(self) -> None:
        root = Path(__file__).parents[1] / "src" / "ai_assistance" / "presentation"
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
            self.assertFalse(any("infrastructure" in name.lower() for name in imported), path)

    def test_reconstitution_calls_are_limited_to_sqlite_persistence(self) -> None:
        root = Path(__file__).parents[1] / "src" / "ai_assistance"
        calls = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in {"reconstitute", "_reconstitute"}:
                        calls.append(path)
        self.assertTrue(calls)
        self.assertTrue(all("infrastructure" in str(path) and "persistence" in str(path) for path in calls), calls)

    def test_application_has_no_reconstitution_reference(self) -> None:
        root = Path(__file__).parents[1] / "src" / "ai_assistance" / "modules" / "price_checker" / "application"
        text = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.py"))
        self.assertNotIn("reconstitute", text)

