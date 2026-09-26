"""Run with stock YAMLGraph: the timed case fails; both controls must pass."""

from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from yamlgraph.compile.graph_loader import compile_graph, load_graph_config


def main():
    for package in ("yamlgraph", "langgraph", "langchain-core"):
        print(f"{package}=={version(package)}")
    source = Path(__file__).with_name("graph.yaml").read_text()
    failures = 0
    with TemporaryDirectory() as directory:
        path = Path(directory) / "graph.yaml"
        for timed in (False, True):
            path.write_text(source if timed else source.replace("    timeout: 2\n", ""))
            app = compile_graph(load_graph_config(path)).compile()
            for sentinel in ("parent-a", "parent-b"):
                try:
                    result = app.invoke({}, {"configurable": {"sentinel": sentinel}})
                    assert result["observed"] == sentinel, result
                    print(f"timeout={timed}, sentinel={sentinel}: PASS")
                except Exception as error:
                    failures += 1
                    print(f"timeout={timed}, sentinel={sentinel}: {type(error).__name__}: {error}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
