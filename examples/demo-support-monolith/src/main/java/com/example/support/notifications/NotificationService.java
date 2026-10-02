package com.example.support.notifications;

import org.springframework.stereotype.Service;

@Service
public class NotificationService {
    private final NotificationRepository repository;

    public NotificationService(NotificationRepository repository) { this.repository = repository; }

    public SupportNotification send(String recipient, String message) {
        return repository.save(new SupportNotification(recipient, message));
    }
}
