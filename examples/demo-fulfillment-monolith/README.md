# Demo fulfillment monolith

This fixture is intentionally harder than the smaller benchmark monoliths. It
contains Orders, Inventory, Shipping, Billing, and Notifications domains plus
shared value objects. Orders and Inventory depend on each other, Shipping reads
the Orders repository directly, and OrderService coordinates multiple writes in
one transaction. These are evidence for review, not problems to hide.

Run the source baseline with:

```powershell
mvn test
```
