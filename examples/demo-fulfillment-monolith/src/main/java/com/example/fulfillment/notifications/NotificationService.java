package com.example.fulfillment.notifications;

import org.springframework.stereotype.Service;

@Service
public class NotificationService {
    public FulfillmentEvent publish(String type) {
        return new FulfillmentEvent(type);
    }
}
