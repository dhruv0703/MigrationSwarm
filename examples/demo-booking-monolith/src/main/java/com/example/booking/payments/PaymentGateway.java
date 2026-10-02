package com.example.booking.payments;

public interface PaymentGateway {
    Payment authorize(String roomCode, String guestId);
}
