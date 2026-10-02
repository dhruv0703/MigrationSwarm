# Demo commerce monolith

This is a deliberately small but realistic Spring Boot 3.3 / Java 17 commerce monolith for MigrationSwarm demonstrations.

It contains Inventory, Orders, Notifications, and Customers domains with controllers, services, repositories, DTOs, JPA entities, shared infrastructure, and tests. Orders calls Inventory through `InventoryGateway`; Notifications is mostly independent but shares audit/event utilities. The application uses H2 and requires no external services.

Run the baseline with:

```powershell
mvn test
```
