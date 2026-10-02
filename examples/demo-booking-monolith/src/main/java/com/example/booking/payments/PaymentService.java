package com.example.booking.payments;

import org.springframework.stereotype.Service;

@Service
public class PaymentService implements PaymentGateway {
    private final PaymentRepository repository;

    public PaymentService(PaymentRepository repository) { this.repository = repository; }

    @Override
    public Payment authorize(String roomCode, String guestId) {
        return repository.save(new Payment(roomCode, guestId));
    }
}
