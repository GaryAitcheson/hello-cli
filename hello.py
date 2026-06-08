import argparse


def greet(name: str) -> str:
    return f"Hello, {name}!"


def main():
    parser = argparse.ArgumentParser(description="A simple greeter.")
    parser.add_argument("--name", default="World", help="Name to greet")
    args = parser.parse_args()
    print(greet(args.name))


if __name__ == "__main__":
    main()
