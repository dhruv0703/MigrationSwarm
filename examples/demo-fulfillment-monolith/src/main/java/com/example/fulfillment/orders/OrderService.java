package com.example.fulfillment.orders;

import com.example.fulfillment.billing.BillingRepository;
import com.example.fulfillment.billing.BillingService;
import com.example.fulfillment.inventory.InventoryRepository;
import com.example.fulfillment.inventory.InventoryService;
import com.example.fulfillment.notifications.NotificationService;
import com.example.fulfillment.shared.Address;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class OrderService {
    private final OrderRepository repository;
    private final InventoryRepository inventoryRepository;
    private final BillingRepository billingRepository;
    private final InventoryService inventory;
    private final BillingService billing;
    private final NotificationService notifications;

    public OrderService(OrderRepository repository, InventoryRepository inventoryRepository,
                        BillingRepository billingRepository, InventoryService inventory,
                        BillingService billing, NotificationService notifications) {
        this.repository = repository;
        this.inventoryRepository = inventoryRepository;
        this.billingRepository = billingRepository;
        this.inventory = inventory;
        this.billing = billing;
        this.notifications = notifications;
    }

    @Transactional
    public Order placeOrder(String sku, Address destination) {
        inventory.reserve(sku);
        billing.charge(sku);
        Order order = repository.save(new Order(sku, null, destination));
        inventoryRepository.flush();
        billingRepository.flush();
        notifications.publish("order.placed");
        return order;
    }
}
