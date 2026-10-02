package com.example.commerce.orders;

import org.springframework.stereotype.Component;

@Component
public class OrderMapper {
    public OrderDto toDto(CustomerOrder order) {
        return new OrderDto(order.getCustomerId(), order.getLines().size());
    }
}
