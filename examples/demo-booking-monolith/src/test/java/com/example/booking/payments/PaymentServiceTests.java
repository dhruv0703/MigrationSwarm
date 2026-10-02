package com.example.booking.payments;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

class PaymentServiceTests {
    @Test
    void paymentStartsAuthorized() {
        assertEquals("AUTHORIZED", new Payment("room", "guest").getStatus());
    }
}
