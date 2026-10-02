package com.example.commerce.notifications;

import java.time.Instant;

import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;

@Entity
public class Notification {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;
    private String recipient;
    private String message;
    private Instant createdAt;

    protected Notification() {
    }

    public Notification(String recipient, String message, Instant createdAt) {
        this.recipient = recipient;
        this.message = message;
        this.createdAt = createdAt;
    }

    public String getRecipient() {
        return recipient;
    }

    public String getMessage() {
        return message;
    }
}
