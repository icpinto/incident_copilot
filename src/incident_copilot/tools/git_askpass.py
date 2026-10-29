"""Git invokes this helper only inside a manually requested sync subprocess."""

import os
import sys

if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]).lower()
    print("x-access-token" if "username" in prompt else os.environ.get("IC_GITHUB_TOKEN", ""))
