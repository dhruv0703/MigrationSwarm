package com.example.fulfillment.shipping;

import com.example.fulfillment.billing.BillingAccount;
import com.example.fulfillment.orders.Order;
import com.example.fulfillment.orders.OrderRepository;
import com.example.fulfillment.shared.Address;
import com.example.fulfillment.shared.FulfillmentRequest;
import org.springframework.stereotype.Service;

@Service
public class ShippingService {
    private final ShippingRepository repository;
    private final OrderRepository orders;

    public ShippingService(ShippingRepository repository, OrderRepository orders) {
        this.repository = repository;
        this.orders = orders;
    }

    public ShippingLabel createLabel(FulfillmentRequest request, BillingAccount account) {
        Order order = orders.findById(Long.valueOf(request.orderId())).orElseThrow();
        Address destination = request.destination();
        return repository.save(new ShippingLabel(account, destination));
    }
}
