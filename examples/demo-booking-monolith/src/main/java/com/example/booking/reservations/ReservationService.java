package com.example.booking.reservations;

import com.example.booking.payments.PaymentGateway;
import com.example.booking.shared.AuditStamp;
import com.example.booking.shared.NotificationPublisher;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class ReservationService {
    private final ReservationRepository repository;
    private final PaymentGateway payments;
    private final NotificationPublisher notifications;
    private final AuditStamp audit;

    public ReservationService(ReservationRepository repository, PaymentGateway payments,
                              NotificationPublisher notifications, AuditStamp audit) {
        this.repository = repository;
        this.payments = payments;
        this.notifications = notifications;
        this.audit = audit;
    }

    @Transactional
    public Reservation reserve(String guestId, String roomCode) {
        Reservation reservation = repository.save(new Reservation(guestId, roomCode));
        payments.authorize(reservation.getRoomCode(), reservation.getGuestId());
        notifications.publish(guestId, "Reservation held for " + roomCode);
        audit.record("reservation.held");
        return reservation;
    }
}
