from graphskill.indexer.extract import extract_file

FIX = "tests/fixtures/sample"


def _names(symbols, kind=None):
    return {s.name for s in symbols if kind is None or s.kind == kind}


def test_python_symbols_and_edges():
    fx = extract_file(f"{FIX}/auth.py", "auth.py")
    assert fx.lang == "python"
    assert {"hash_password", "BaseUser", "User", "login", "name"} <= _names(fx.symbols)
    # class contains its method
    assert any(
        p.endswith(("#149", "#209")) for p, _ in fx.contains
    ), "expected CONTAINS parent symbols"
    # inheritance recorded by base name
    assert ("User" in {s.name for s in fx.symbols})
    assert any(base == "BaseUser" for _, base in fx.inherits)
    # import + call sites
    assert "db" in fx.imports
    callees = {cs.callee_name for cs in fx.callsites}
    assert {"connect", "hash_password"} <= callees


def test_typescript_and_go():
    ts = extract_file(f"{FIX}/service.ts", "service.ts")
    assert "Session" in _names(ts.symbols, "class")
    assert "login" in {cs.callee_name for cs in ts.callsites}
    assert "./auth" in ts.imports

    go = extract_file(f"{FIX}/handler.go", "handler.go")
    assert {"Handle", "process"} <= _names(go.symbols)
    assert "process" in {cs.callee_name for cs in go.callsites}


def test_php_full_tier(tmp_path):
    f = tmp_path / "Cors.php"
    f.write_text(
        "<?php\n"
        "namespace App\\Http;\n"
        "use Psr\\Http\\Server\\MiddlewareInterface;\n"
        "class Cors implements MiddlewareInterface {\n"
        "    public function process() { return $this->addHeaders(); }\n"
        "    private function addHeaders() { return 1; }\n"
        "}\n"
    )
    fx = extract_file(f, "Cors.php")
    assert fx.lang == "php"
    assert _names(fx.symbols, "class") == {"Cors"}
    assert {"process", "addHeaders"} <= _names(fx.symbols, "method")
    assert any(base == "MiddlewareInterface" for _, base in fx.inherits)
    assert "Psr\\Http\\Server\\MiddlewareInterface" in fx.imports
    assert "addHeaders" in {cs.callee_name for cs in fx.callsites}


def test_php_uses_edges(tmp_path):
    f = tmp_path / "Svc.php"
    f.write_text(
        "<?php\n"
        "namespace App;\n"
        "class Svc {\n"
        "    public function __construct(private Logger $log) {}\n"
        "    public function run(): Result {\n"
        "        $r = new Helper();\n"
        "        return Factory::make();\n"
        "    }\n"
        "}\n"
    )
    fx = extract_file(f, "Svc.php")
    referenced = {name for _, name in fx.uses}
    # type-hint (Logger), return type (Result), new (Helper), static scope (Factory)
    assert {"Logger", "Result", "Helper", "Factory"} <= referenced
    assert "Svc" not in referenced  # no self-reference


def test_python_uses_edges(tmp_path):
    f = tmp_path / "svc.py"
    f.write_text(
        "class Repo:\n"
        "    pass\n"
        "\n"
        "class Service:\n"
        "    def process(self, repo: Repo) -> Repo:\n"
        "        return repo\n"
    )
    fx = extract_file(f, "svc.py")
    referenced = {name for _, name in fx.uses}
    assert "Repo" in referenced
    assert "Service" not in referenced  # no self-reference


def test_typescript_uses_edges():
    fx = extract_file(f"{FIX}/service.ts", "service.ts")
    referenced = {name for _, name in fx.uses}
    assert "User" in referenced


def test_generic_fallback_for_unqueried_language(tmp_path):
    # C and Ruby have no per-language DEFS entry -> generic fallback must still
    # yield symbols (the language-agnostic guarantee).
    c = tmp_path / "lib.c"
    c.write_text("int add(int a, int b) {\n    return a + b;\n}\nstruct Point { int x; };\n")
    fx = extract_file(c, "lib.c")
    assert _names(fx.symbols) >= {"add", "Point"}

    rb = tmp_path / "svc.rb"
    rb.write_text("class Account\n  def deposit(n)\n    n\n  end\nend\n")
    fx = extract_file(rb, "svc.rb")
    assert _names(fx.symbols) >= {"Account", "deposit"}


def test_unknown_language_returns_none():
    assert extract_file("README.md", "README.md") is None
