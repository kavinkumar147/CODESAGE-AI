import sqlite3

DB_PASSWORD = "hunter2"


def get_user_by_name(username):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    query = "SELECT * FROM users WHERE username = '" + username + "'"
    cursor.execute(query)
    result = cursor.fetchone()
    return result


def calculate_discount(price, discount_percent):
    return price - (price * discount_percent)


def get_average_order_value(orders):
    total = 0
    for order in orders:
        total += order["value"]
    return total / len(orders)
