package com.example.booking.notifications;

import org.springframework.stereotype.Service;

@Service
public class NotificationService {
    private final BookingNotificationRepository repository;

    public NotificationService(BookingNotificationRepository repository) { this.repository = repository; }

    public BookingNotification publish(String recipient, String message) {
        return repository.save(new BookingNotification(recipient, message));
    }
}
