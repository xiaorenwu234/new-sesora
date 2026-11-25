"""Online Boutique 业务工作负载（Locust，固定场景统一实验条件）。

覆盖关键业务路径：浏览 -> 商品 -> 加购 -> 查看购物车 -> 下单结算。
"""
import random

from locust import HttpUser, task, between

PRODUCT_IDS = ["OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O", "L9ECAV7KIM",
               "2ZYFJ3GM2N", "0PUK6V6EV0", "LS4PSXUNUM", "9SIQT8TOJO", "6E92ZMYYFZ"]

CHECKOUT_FORM = {
    "email": "loadtest@example.com",
    "street_address": "1600 Amphitheatre Parkway",
    "zip_code": "94043",
    "city": "Mountain View",
    "state": "CA",
    "country": "United States",
    "credit_card_number": "4432-8015-6152-0454",
    "credit_card_expiration_month": "1",
    "credit_card_expiration_year": "2030",
    "credit_card_cvv": "672",
}


class BoutiqueUser(HttpUser):
    wait_time = between(1, 5)

    @task(10)
    def browse(self):
        self.client.get("/", name="/")
        self.client.get(f"/product/{random.choice(PRODUCT_IDS)}", name="/product/[id]")

    @task(4)
    def cart_flow(self):
        pid = random.choice(PRODUCT_IDS)
        self.client.get(f"/product/{pid}", name="/product/[id]")
        self.client.post("/cart", data={"product_id": pid, "quantity": 1}, name="/cart add")
        self.client.get("/cart", name="/cart")

    @task(2)
    def checkout(self):
        pid = random.choice(PRODUCT_IDS)
        self.client.post("/cart", data={"product_id": pid, "quantity": 1}, name="/cart add")
        with self.client.post("/cart/checkout", data=CHECKOUT_FORM,
                              name="/cart/checkout", catch_response=True) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"checkout status {resp.status_code}")

    @task(2)
    def currency_browse(self):
        self.client.get("/product/{0}".format(random.choice(PRODUCT_IDS)),
                        name="/product/[id] currency")