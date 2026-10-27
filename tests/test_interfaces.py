from textwrap import dedent

from pr_sift.interfaces import annotate, diff_interfaces, extract_interfaces
from pr_sift.noise import classify
from pr_sift.units import diff_files


def changes(old, new, path="pkg/mod.py"):
    old_files = {path: dedent(old)} if old is not None else {}
    new_files = {path: dedent(new)} if new is not None else {}
    return [(c.category, c.name, c.status, c.before, c.after) for c in diff_interfaces(old_files, new_files)]


def test_public_signature_change():
    assert changes(
        """
        def fetch(url, timeout=10):
            pass

        def _private(a):
            pass
        """,
        """
        def fetch(url, *, timeout=10, retry=None) -> bytes:
            pass

        def _private(a, b):
            pass
        """,
    ) == [
        (
            "signature",
            "pkg.mod.fetch",
            "changed",
            "def fetch(url, timeout=10)",
            "def fetch(url, *, timeout=10, retry=None) -> bytes",
        )
    ]


def test_formatting_does_not_change_signatures():
    assert changes("def f(a,b = 1):\n    pass\n", "def f(\n    a,\n    b=1,\n):\n    pass\n") == []


def test_method_and_class_signatures():
    result = changes(
        """
        class Client:
            def get(self, url):
                pass
        """,
        """
        class Client(Base):
            @property
            def get(self):
                pass
        """,
    )
    assert result == [
        ("signature", "pkg.mod.Client", "changed", "class Client", "class Client(Base)"),
        ("signature", "pkg.mod.Client.get", "changed", "def get(self, url)", "@property def get(self)"),
    ]


def test_dunder_all_makes_private_names_public():
    assert changes("__all__ = ['_f']\n", "__all__ = ['_f']\n\ndef _f():\n    pass\n") == [
        ("signature", "pkg.mod._f", "added", None, "def _f()"),
    ]


def test_model_fields():
    result = changes(
        """
        @dataclass
        class User:
            name: str
            age: int = 0
        """,
        """
        @dataclass
        class User:
            name: str
            age: int | None = None
            email: str = ""
        """,
    )
    assert [r for r in result if r[0] == "model-field"] == [
        ("model-field", "pkg.mod.User.age", "changed", "age: int = 0", "age: int | None = None"),
        ("model-field", "pkg.mod.User.email", "added", None, "email: str = ''"),
    ]


def test_sqlalchemy_and_enum_fields():
    names = {
        i.name
        for i in extract_interfaces(
            "m.py",
            dedent(
                """
                class Order(Base):
                    __tablename__ = "orders"
                    id = Column(Integer, primary_key=True)
                    note = "not a field"

                class Color(Enum):
                    RED = 1
                """
            ),
        )
        if i.category == "model-field"
    }
    assert names == {"m.Order.id", "m.Color.RED"}


def test_routes():
    assert changes(
        """
        @router.get("/users/{id}")
        def get_user(id):
            pass
        """,
        """
        @router.get("/users/{id}")
        def get_user(id):
            pass

        @app.route("/users", methods=["POST", "PUT"])
        def save_user():
            pass
        """,
    ) == [
        ("signature", "pkg.mod.save_user", "added", None, "def save_user()"),
        ("route", "POST /users", "added", None, "app.route('/users', methods=['POST', 'PUT'])"),
        ("route", "PUT /users", "added", None, "app.route('/users', methods=['POST', 'PUT'])"),
    ]


def test_same_route_in_two_modules_is_not_merged():
    old = {"a.py": '@r.get("/")\ndef a():\n    pass\n', "b.py": '@r.get("/")\ndef b():\n    pass\n'}
    new = {"a.py": '@r.get("/")\ndef a():\n    pass\n', "b.py": ""}
    routes = [c for c in diff_interfaces(old, new) if c.category == "route"]
    assert [(c.name, c.status, c.path) for c in routes] == [("GET /", "removed", "b.py")]


def test_cli_click_and_argparse():
    result = changes(
        """
        @cli.command()
        @click.option("--count", default=1)
        def greet(count):
            pass

        def main():
            parser.add_argument("--verbose", action="store_true")
        """,
        """
        @cli.command("hello")
        @click.option("--count", "-c", default=1)
        def greet(count):
            pass

        def main():
            parser.add_argument("--quiet", action="store_true")
        """,
    )
    assert [(r[1], r[2]) for r in result if r[0] == "cli"] == [
        ("command greet", "removed"),
        ("command hello", "added"),
        ("greet: --count", "removed"),
        ("greet: --count / -c", "added"),
        ("main: --quiet", "added"),
        ("main: --verbose", "removed"),
    ]


def test_env_vars():
    assert changes(
        """
        def config():
            return os.environ["API_KEY"], os.getenv("TIMEOUT", "10")
        """,
        """
        def config():
            return os.environ.get("API_KEY"), os.getenv("TIMEOUT", "30"), os.environ["REGION"]
        """,
    ) == [
        ("env", "API_KEY", "changed", "required", "default=None"),
        ("env", "REGION", "added", None, "required"),
        ("env", "TIMEOUT", "changed", "default='10'", "default='30'"),
    ]


def test_pyproject_dependencies():
    old = '[project]\ndependencies = ["requests>=2"]\n\n[dependency-groups]\ndev = ["pytest"]\n'
    new = '[project]\ndependencies = ["requests>=2.31", "httpx"]\n\n[dependency-groups]\ndev = ["pytest"]\n'
    assert changes(old, new, path="pyproject.toml") == [
        ("dependency", "httpx", "added", None, "httpx"),
        ("dependency", "requests", "changed", "requests>=2", "requests>=2.31"),
    ]


def test_requirements_file():
    assert changes("flask==2.0  # web\n", "flask==3.0\n-r base.txt\n", path="requirements-dev.txt") == [
        ("dependency", "flask", "changed", "flask==2.0", "flask==3.0"),
    ]


def test_new_migration():
    assert changes(None, "revision = 'abc'\n", path="alembic/versions/abc_add_users.py") == [
        ("migration", "alembic/versions/abc_add_users.py", "added", None, ""),
    ]


def test_annotate_adds_signals_to_units():
    old = {"pkg/mod.py": "def fetch(url):\n    return os.getenv('A')\n", "pyproject.toml": "[project]\ndependencies = []\n"}
    new = {"pkg/mod.py": "def fetch(url, retry):\n    return os.getenv('B')\n", "pyproject.toml": "[project]\ndependencies = ['x']\n"}
    units = classify(diff_files(old, new))
    annotate(units, diff_interfaces(old, new))
    assert {u.qualname: u.signals for u in units} == {
        "pkg.mod.fetch": ["public-signature", "env"],
        "pyproject.toml": ["dependency"],
    }


def test_model_field_comments():
    source = dedent(
        """
        @dataclass
        class Case:
            # What to call,
            # relative to the base URL
            path: str
            timeout: float = 30  # seconds
            name: str
        """
    )
    docs = {c.name: c.doc for c in diff_interfaces({}, {"m.py": source}) if c.category == "model-field"}
    assert docs == {"m.Case.path": "What to call, relative to the base URL", "m.Case.timeout": "seconds", "m.Case.name": None}


def test_tests_are_not_interfaces():
    source = "def test_it():\n    assert os.environ['X']\n"
    for path in ("tests/unit/test_a.py", "pkg/test_a.py", "pkg/a_test.py", "tests/conftest.py"):
        assert extract_interfaces(path, source) == []
    assert extract_interfaces("pkg/testing.py", source) != []
