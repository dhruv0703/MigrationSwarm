package com.example.commerce.notifications;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;

import com.example.commerce.shared.SharedEventPublisher;
import org.junit.jupiter.api.Test;

class NotificationServiceTests {
    @Test
    void createsOrderNotification() {
        NotificationRepository repository = mock(NotificationRepository.class);
        Notification notification = new Notification("buyer@example.test", "Order O-1 was placed", Instant.EPOCH);
        when(repository.save(org.mockito.ArgumentMatchers.any())).thenReturn(notification);
        NotificationService service = new NotificationService(
                repository,
                new NotificationMapper(),
                new NotificationTemplate(),
                new SharedEventPublisher(),
                Clock.fixed(Instant.EPOCH, ZoneOffset.UTC));

        NotificationDto result = service.notifyOrder("buyer@example.test", "O-1");

        assertThat(result.message()).contains("O-1");
    }
}
