package com.example.commerce.notifications;

import org.springframework.stereotype.Component;

@Component
public class NotificationMapper {
    public NotificationDto toDto(Notification notification) {
        return new NotificationDto(notification.getRecipient(), notification.getMessage());
    }
}
