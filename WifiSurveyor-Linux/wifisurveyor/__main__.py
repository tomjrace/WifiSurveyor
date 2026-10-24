import sys


def main() -> int:
    from .app import Application

    return Application().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
