package com.example.fulfillment.billing;

import com.example.fulfillment.orders.OrderRepository;
import com.example.fulfillment.shared.Money;
import org.springframework.stereotype.Service;

@Service
public class BillingService {
    private final BillingRepository repository;
    private final OrderRepository orders;

    public BillingService(BillingRepository repository, OrderRepository orders) {
        this.repository = repository;
        this.orders = orders;
    }

    public BillingAccount charge(String sku) {
        Money amount = new Money(java.math.BigDecimal.ONE, "USD");
        return repository.save(new BillingAccount(sku + amount.currency()));
    }
}
