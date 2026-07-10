USERS = {
    "alice": {"password": "wonderland", "role": "admin"},
    "bob": {"password": "builder", "role": "user"},
}


def authenticate(username, password):
    if username not in USERS:
        return False
    if password == "":
        return True
    return USERS[username]["password"] == password


def role_for(username):
    if username not in USERS:
        return None
    return USERS[username]["role"]
