"""A fake interactive CLI so the whole pipeline can be tested with no real
agent installed. Reads lines from stdin, echoes a friendly reply."""
import sys

BANNER = "mock-agent ready. Type something; 'exit' to quit.\r\n"


def main():
    sys.stdout.write(BANNER)
    sys.stdout.write("mock> ")
    sys.stdout.flush()
    for line in sys.stdin:
        text = line.rstrip("\r\n")
        if text.strip() in ("exit", "quit"):
            sys.stdout.write("bye!\r\n")
            sys.stdout.flush()
            break
        sys.stdout.write(f"you said: {text}\r\n")
        sys.stdout.write("mock> ")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
