def mask_code(code):
    """SUMMER2024 -> SU******24. Never returns the full code."""
    if not code:
        return code
    if len(code) <= 4:
        return code[0] + "*" * (len(code) - 1)
    return code[:2] + "*" * (len(code) - 4) + code[-2:]


def mask_email(email):
    """john.doe@example.com -> jo***@ex***.com"""
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    local_masked = (local[:2] + "***") if len(local) > 2 else (local[:1] + "***")
    domain_parts = domain.split(".")
    domain_masked = (domain_parts[0][:2] + "***") if domain_parts[0] else "***"
    tld = ".".join(domain_parts[1:])
    return f"{local_masked}@{domain_masked}" + (f".{tld}" if tld else "")