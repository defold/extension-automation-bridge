"""Update a project's fetched Python wrapper without configuring PYTHONPATH."""

import argparse
from automation_bridge import editor


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_path", help="Defold project directory; Fetch Libraries first")
    args = parser.parse_args()
    try:
        print(editor.update_python_wrapper(args.project_path))
    except (editor.Error, OSError, ValueError) as exc:
        parser.exit(1, f"Cannot update Automation Bridge Python wrapper: {exc}\n")


if __name__ == "__main__":
    main()
