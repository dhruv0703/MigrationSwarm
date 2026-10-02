package com.example.monolith;

import org.springframework.stereotype.Repository;

@Repository
public class GreetingRepository {
    public String findGreeting() {
        return "Hello from MigrationSwarm";
    }
}
