package com.example.commerce.shared;

import java.time.Instant;

public record DomainEvent(String name, String aggregateId, Instant occurredAt) {
}
