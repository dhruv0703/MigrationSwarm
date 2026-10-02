package com.example.fulfillment.inventory;

import com.example.fulfillment.orders.OrderService;
import org.springframework.stereotype.Service;

@Service
public class InventoryService {
    private final InventoryRepository repository;
    private final OrderService orders;

    public InventoryService(InventoryRepository repository, OrderService orders) {
        this.repository = repository;
        this.orders = orders;
    }

    public InventoryItem reserve(String sku) {
        return repository.save(new InventoryItem(sku));
    }

    public String reservationOwner() {
        return orders.getClass().getSimpleName();
    }
}
