package com.example.monolith;

import org.springframework.stereotype.Service;

@Service
public class GreetingService {
    private final GreetingRepository repository;

    public GreetingService(GreetingRepository repository) {
        this.repository = repository;
    }

    public String greeting() {
        return repository.findGreeting();
    }
}
