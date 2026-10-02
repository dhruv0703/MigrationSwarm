# Demo Booking Monolith

A Spring Boot booking fixture with Reservations, Payments, and Notifications.
Reservations coordinate payment authorization and shared notification publishing;
the payment record also keeps a reservation reference to expose a cross-domain
data coupling case.

```powershell
mvn test -q
```
