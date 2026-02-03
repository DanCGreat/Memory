TEST_USERS: set[int] = set()

def toggle_test_mode(user_id: int) -> bool:
    if user_id in TEST_USERS:
        TEST_USERS.remove(user_id)
        return False
    TEST_USERS.add(user_id)
    return True

def is_test_mode(user_id: int) -> bool:
    return user_id in TEST_USERS
