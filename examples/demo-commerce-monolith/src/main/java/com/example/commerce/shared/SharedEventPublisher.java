package com.example.commerce.shared;

import org.springframework.stereotype.Component;

@Component
public class SharedEventPublisher {
    public void publish(DomainEvent event) {
        // The monolith keeps publishing in-process for this local demonstration.
    }
}
