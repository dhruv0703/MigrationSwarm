package com.example.commerce.shared;

import java.time.Instant;

import jakarta.persistence.Embeddable;

@Embeddable
public class AuditStamp {
    private Instant createdAt;
    private Instant updatedAt;

    protected AuditStamp() {
    }

    public AuditStamp(Instant createdAt) {
        this.createdAt = createdAt;
        this.updatedAt = createdAt;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }

    public Instant getUpdatedAt() {
        return updatedAt;
    }
}
