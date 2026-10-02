package com.example.commerce.orders;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;

import com.example.commerce.inventory.InventoryItemDto;
import com.example.commerce.shared.SharedEventPublisher;
import org.junit.jupiter.api.Test;

class OrderServiceTests {
    private static class StubInventoryService extends com.example.commerce.inventory.InventoryService {
        StubInventoryService() {
            super(null, null, null, null);
        }

        @Override
        public InventoryItemDto reserve(String sku, int quantity) {
            return new InventoryItemDto(sku, 99);
        }
    }

    @Test
    void placesOrderAfterInventoryReservation() {
        OrderRepository repository = mock(OrderRepository.class);
        CustomerOrder order = new CustomerOrder(
                "customer-1",
                java.util.List.of(new OrderLine("SKU-1", 1)),
                new com.example.commerce.shared.AuditStamp(Instant.EPOCH));
        org.mockito.Mockito.when(repository.save(org.mockito.ArgumentMatchers.any())).thenReturn(order);
        InventoryGateway inventory = new InventoryGateway(new StubInventoryService());
        OrderService service = new OrderService(
                repository,
                new OrderMapper(),
                inventory,
                new SharedEventPublisher(),
                Clock.fixed(Instant.EPOCH, ZoneOffset.UTC));

        OrderDto result = service.place("customer-1", "SKU-1", 1);

        assertThat(result.lineCount()).isEqualTo(1);
    }
}
