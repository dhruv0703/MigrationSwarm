package com.example.commerce.inventory;

import org.springframework.stereotype.Component;

@Component
public class InventoryMapper {
    public InventoryItemDto toDto(InventoryItem item) {
        return new InventoryItemDto(item.getSku(), item.getAvailableQuantity());
    }
}
