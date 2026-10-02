package com.example.booking.reservations;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

class ReservationServiceTests {
    @Test
    void reservationStartsHeld() {
        assertEquals("HELD", new Reservation("guest", "room").getStatus());
    }
}
