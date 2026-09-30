"""Print a password hash for S3_WEBADMIN_PASSWORD_HASH."""
import getpass
import sys

from werkzeug.security import generate_password_hash

if __name__ == "__main__":
    pw = getpass.getpass("New web admin password: ")
    if len(pw) < 8:
        sys.exit("Use at least 8 characters.")
    if pw != getpass.getpass("Repeat: "):
        sys.exit("Passwords do not match.")
    print(generate_password_hash(pw))
