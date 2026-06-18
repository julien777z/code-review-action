import asyncio
import sys

from code_review.runtime import main

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
