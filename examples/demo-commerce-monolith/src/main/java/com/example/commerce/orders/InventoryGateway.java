package com.example.commerce.orders;

import com.example.commerce.inventory.InventoryItemDto;
import com.example.commerce.inventory.InventoryService;
import org.springframework.stereotype.Component;

@Component
public class InventoryGateway {
    private final InventoryService inventoryService;

    public InventoryGateway(InventoryService inventoryService) {
        this.inventoryService = inventoryService;
    }

    public InventoryItemDto reserve(String sku, int quantity) {
        return inventoryService.reserve(sku, quantity);
    }
}
