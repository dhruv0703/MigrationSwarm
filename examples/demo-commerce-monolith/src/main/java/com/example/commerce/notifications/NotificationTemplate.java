package com.example.commerce.notifications;

import org.springframework.stereotype.Component;

@Component
public class NotificationTemplate {
    public String orderConfirmation(String orderReference) {
        return "Order " + orderReference + " was placed";
    }
}
