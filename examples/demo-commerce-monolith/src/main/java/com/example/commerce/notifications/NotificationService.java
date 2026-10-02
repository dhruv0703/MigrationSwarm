package com.example.commerce.notifications;

import java.time.Clock;
import java.time.Instant;

import com.example.commerce.shared.DomainEvent;
import com.example.commerce.shared.SharedEventPublisher;
import org.springframework.stereotype.Service;

@Service
public class NotificationService {
    private final NotificationRepository repository;
    private final NotificationMapper mapper;
    private final NotificationTemplate template;
    private final SharedEventPublisher events;
    private final Clock clock;

    public NotificationService(
            NotificationRepository repository,
            NotificationMapper mapper,
            NotificationTemplate template,
            SharedEventPublisher events,
            Clock clock) {
        this.repository = repository;
        this.mapper = mapper;
        this.template = template;
        this.events = events;
        this.clock = clock;
    }

    public NotificationDto notifyOrder(String recipient, String orderReference) {
        Notification notification = repository.save(new Notification(
                recipient,
                template.orderConfirmation(orderReference),
                Instant.now(clock)));
        events.publish(new DomainEvent("notification.created", recipient, Instant.now(clock)));
        return mapper.toDto(notification);
    }
}
