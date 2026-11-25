"""Astronomy Shop（OpenTelemetry Demo）业务工作负载（Locust）。

覆盖关键业务路径：浏览商品 -> 加购 -> 查看购物车 -> 下单结算。
frontend 为 Next.js，业务 API 走 /api/* 路由。
"""
import random
import uuid

from locust import HttpUser, task, between

# Astronomy Shop 商品 ID（product-catalog 固定数据）
PRODUCT_IDS = ["OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O", "L9ECAV7KIM",
               "2ZYFJ3GM2N", "0PUK6V6EV0", "LS4PSXUNUM", "9SIQT8TOJO", "6E92ZMYYFZ"]


class AstronomyUser(HttpUser):
    wait_time = between(1, 5)

    @task(10)
    def browse(self):
        self.client.get("/", name="/")
        self.client.get("/api/products", name="/api/products")
        self.client.get(f"/api/products/{random.choice(PRODUCT_IDS)}",
                        name="/api/products/[id]")

    @task(4)
    def cart_flow(self):
        pid = random.choice(PRODUCT_IDS)
        self.client.get(f"/api/products/{pid}", name="/api/products/[id]")
        self.client.post(f"/api/cart?sessionId={uuid.uuid4()}",
                         json={"item": {"productId": pid, "quantity": 1}},
                         name="/api/cart add")
        self.client.get("/api/cart", name="/api/cart")

    @task(2)
    def checkout(self):
        pid = random.choice(PRODUCT_IDS)
        sid = str(uuid.uuid4())
        self.client.post(f"/api/cart?sessionId={sid}",
                         json={"item": {"productId": pid, "quantity": 1}},
                         name="/api/cart add")
        with self.client.post("/api/checkout",
                              json={"email": "loadtest@example.com",
                                    "address": {"streetAddress": "1600 Amphitheatre Parkway",
                                                "city": "Mountain View", "state": "CA",
                                                "country": "United States", "zipCode": "94043"},
                                    "userCurrency": "USD",
                                    "creditCard": {"creditCardNumber": "4432-8015-6152-0454",
                                                   "creditCardCvv": 672,
                                                   "creditCardExpirationMonth": 1,
                                                   "creditCardExpirationYear": 2030}},
                              name="/api/checkout", catch_response=True) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"checkout status {resp.status_code}")