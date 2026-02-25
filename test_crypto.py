from vaf.auth.crypto import hash_password, verify_password

def test():
    pw = "Mert1997can"
    h = hash_password(pw)
    print(f"Hash: {h}")
    match = verify_password(h, pw)
    print(f"Match: {match}")

if __name__ == "__main__":
    test()
