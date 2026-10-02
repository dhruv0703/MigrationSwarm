package com.example.fulfillment.shared;

public record FulfillmentRequest(String orderId, Address destination) {
}
