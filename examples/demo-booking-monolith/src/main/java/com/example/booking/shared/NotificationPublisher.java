package com.example.booking.shared;

import com.example.booking.notifications.NotificationService;
import org.springframework.stereotype.Component;

@Component
public class NotificationPublisher {
    private final NotificationService service;

    public NotificationPublisher(NotificationService service) { this.service = service; }

    public void publish(String recipient, String message) { service.publish(recipient, message); }
}
