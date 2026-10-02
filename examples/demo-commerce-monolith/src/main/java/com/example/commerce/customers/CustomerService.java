package com.example.commerce.customers;

import org.springframework.stereotype.Service;

@Service
public class CustomerService {
    private final CustomerRepository repository;

    public CustomerService(CustomerRepository repository) {
        this.repository = repository;
    }

    public Customer findOrCreate(String externalId, String email) {
        return repository.findByExternalId(externalId)
                .orElseGet(() -> repository.save(new Customer(externalId, email)));
    }
}
