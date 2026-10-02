package com.example.commerce.inventory;

import java.time.Clock;
import java.time.Instant;

import com.example.commerce.shared.AuditStamp;
import com.example.commerce.shared.DomainEvent;
import com.example.commerce.shared.SharedEventPublisher;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class InventoryService {
    private final InventoryRepository repository;
    private final InventoryMapper mapper;
    private final SharedEventPublisher events;
    private final Clock clock;

    public InventoryService(
            InventoryRepository repository,
            InventoryMapper mapper,
            SharedEventPublisher events,
            Clock clock) {
        this.repository = repository;
        this.mapper = mapper;
        this.events = events;
        this.clock = clock;
    }

    @Transactional
    public InventoryItemDto reserve(String sku, int quantity) {
        InventoryItem item = repository.findBySku(sku)
                .orElseGet(() -> repository.save(new InventoryItem(sku, 100, new AuditStamp(Instant.now(clock)))));
        item.reserve(quantity);
        events.publish(new DomainEvent("inventory.reserved", sku, Instant.now(clock)));
        return mapper.toDto(item);
    }
}
