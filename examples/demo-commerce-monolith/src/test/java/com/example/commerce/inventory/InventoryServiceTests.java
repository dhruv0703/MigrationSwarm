package com.example.commerce.inventory;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;

import com.example.commerce.shared.SharedEventPublisher;
import org.junit.jupiter.api.Test;

class InventoryServiceTests {
    @Test
    void reservesExistingInventory() {
        InventoryRepository repository = mock(InventoryRepository.class);
        InventoryMapper mapper = new InventoryMapper();
        InventoryItem item = new InventoryItem("SKU-1", 4, new com.example.commerce.shared.AuditStamp(Instant.EPOCH));
        when(repository.findBySku("SKU-1")).thenReturn(java.util.Optional.of(item));
        InventoryService service = new InventoryService(
                repository,
                mapper,
                new SharedEventPublisher(),
                Clock.fixed(Instant.EPOCH, ZoneOffset.UTC));

        InventoryItemDto result = service.reserve("SKU-1", 2);

        assertThat(result.availableQuantity()).isEqualTo(2);
    }
}
