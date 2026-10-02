package com.example.commerce.orders;

import java.time.Clock;
import java.time.Instant;
import java.util.List;

import com.example.commerce.shared.AuditStamp;
import com.example.commerce.shared.DomainEvent;
import com.example.commerce.shared.SharedEventPublisher;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class OrderService {
    private final OrderRepository repository;
    private final OrderMapper mapper;
    private final InventoryGateway inventory;
    private final SharedEventPublisher events;
    private final Clock clock;

    public OrderService(
            OrderRepository repository,
            OrderMapper mapper,
            InventoryGateway inventory,
            SharedEventPublisher events,
            Clock clock) {
        this.repository = repository;
        this.mapper = mapper;
        this.inventory = inventory;
        this.events = events;
        this.clock = clock;
    }

    @Transactional
    public OrderDto place(String customerId, String sku, int quantity) {
        inventory.reserve(sku, quantity);
        CustomerOrder order = new CustomerOrder(
                customerId,
                List.of(new OrderLine(sku, quantity)),
                new AuditStamp(Instant.now(clock)));
        CustomerOrder saved = repository.save(order);
        events.publish(new DomainEvent("order.placed", customerId, Instant.now(clock)));
        return mapper.toDto(saved);
    }
}
