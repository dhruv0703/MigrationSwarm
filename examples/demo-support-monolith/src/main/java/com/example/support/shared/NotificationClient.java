package com.example.support.shared;

import com.example.support.notifications.NotificationService;
import org.springframework.stereotype.Component;

@Component
public class NotificationClient {
    private final NotificationService service;

    public NotificationClient(NotificationService service) { this.service = service; }

    public void send(String recipient, String message) { service.send(recipient, message); }
}
