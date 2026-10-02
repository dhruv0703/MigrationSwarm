# Demo Support Monolith

A realistic Spring Boot support fixture with Tickets, Users, and Notifications.
Tickets depend on the user directory and a shared notification client. The
shared package is intentionally not an expected service boundary.

```powershell
mvn test -q
```
